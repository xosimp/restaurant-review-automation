# Database Schema — Cavnar AI

Single SQLite database (`reviews.db`), WAL mode, on a Railway persistent volume in production. Schema lives across `models.py` (`init_db()` + `ensure_columns()` + a dozen `init_*()` helpers for tables that didn't exist from day one), `auth.py` (`init_auth()`), `push.py`, `webhooks.py`, `guest_marketing.py`, `sales_audits.py`, `ops.py`, `ai_utils.py` and a few single-table owners (`admin_events`, `milestones`, `home_brief`). All of it runs at boot from `hosted_dashboard.py`. There is no version table — migrations are additive `ALTER TABLE ADD COLUMN` statements guarded by `try/except`. **116 tables** on 2026-09-21; the count is the `CREATE TABLE IF NOT EXISTS` names across non-test Python (`python3 scripts/repo_inventory.py`), and this file lists them by area rather than re-deriving each one.

`restaurant_id` is the tenant boundary on essentially every table below it — every read must filter by it from the authenticated session, never from client-supplied input.

## Core identity

### `restaurants` (models.py)
The tenant row (191 columns). Everything else hangs off `restaurants.id`. The list below is by purpose, not exhaustive; the authoritative column set is the four touch points — the `Restaurant` dataclass, the migration lists (`init_db()`'s ALTERs and `ensure_columns()`), `update_restaurant().allowed`, and `get_restaurant()`'s hydration — and a column missing from any one of them is a bug, not a documentation gap. Not itemised here: the per-type alert channel matrix (`alert_*`, `al_<type>_email|sms|push`, `alert_quiet_*`, `alert_max_per_day`), every POS/OAuth credential column (`toast_*`, `square_*`, `clover_*`, `rpower_*`, `gmb_*`, `ig_*`/`fb_*` — encrypted at rest by `credentials.py`), the 2FA columns, the automation switches (`auto_*`, `send_delay_minutes`, `weekly_plan_enabled`), `category` (the intelligence cohort), `organization_id`, `row_version`, `paused_until`, `deletion_requested_at`.
- **Identity/contact**: `name`, `owner_email`, `owner_name`, `owner_phone`, `google_place_id`, `yelp_business_id`
- **Brand voice**: `voice_notes`, `neighborhood`, `vibe`, `known_for`, `sign_off_name`, `never_say`
- **Labor settings**: `hourly_rate`, `labor_target_pct`, `week_start_day` (0=Mon, for FLSA overtime on the employer's own workweek), scheduling JSON blobs (`role_rates_json`, `close_times_json`, `role_close_buffer_json`, `role_minimums_json`, `role_strength_json`, `shift_leader_rules_json`, `quality_weights_json`), `sched_notes`
- **Location grouping**: `location_group`, `location_name` (multi-location clients)
- **Food cost**: `inventory_frequency`, `delivery_days`, `inventory_notes`, `food_cost_target`, `inventory_updated_at`
- **Billing/contract**: `stripe_customer_id`, `docusign_envelope_id`, `contract_status` (pending/sent/signed), `billing_status` (trial/active/paused/churned)
- **Access control**: `service_tier` (trial/starter_reviews/starter_labor/starter_inventory/starter_marketing/full) auto-sets `module_reviews`/`module_labor`/`module_inventory`/`module_marketing` booleans, which can be overridden per client
- **Ops**: `is_demo` (seed data may be reset — never true for a real client), `internal_notes` (Will-only), `pos_system`, `digest_day`/`digest_enabled`, `last_fetched_at`, `last_active_tab`, `last_activity`
- **Misc computed/cached**: `latitude`/`longitude`/`weather_cache_json`/`weather_cached_at` (geocoded once, cached forecast), `email_theme`, `marketing_emails_opt_out`, `timezone`

### `users` / `sessions` / `login_history` (auth.py)
- `users`: one row per login, `restaurant_id` FK, `username`/`email` unique, `password_hash`, `is_admin`, `is_active`, `role` ∈ `permissions.py`'s roles — `owner` (multi-location), `client` (the primary login and DB default; shown as Co-owner), `manager`, `member` (Teammate), `employee` (PIN identity, staff portal only), `support` (Cavnar staff, read-only admin).
- `sessions`: `token` PK, `expires_at`, `last_active`, `ip_address`, `user_agent` — hard-deleted on expiry/revoke/device-dedup.
- `login_history`: append-only, **never** pruned — independent of `sessions`, this is what the Account "sign-in activity" view reads, so a login stays visible regardless of what later happens to the session it produced.
- `trusted_devices`: "remember this device 30 days" for 2FA, one row per device (superseded a single-column approach that only held the last device).
- `two_fa_backup_codes`: hashed, single-use fallback codes generated at 2FA setup.
- `login_attempts` (models.py, used by `security.py`): durable login throttling by account and by IP; rows older than two days are pruned by the scheduler.
- `organizations` + `restaurants.organization_id`: the owner-level grouping above a location group (backfilled from `location_group` + `owner_email`).

### Staff portal (auth.py `AUTH_SCHEMA`)
- `memberships`: an employee's identity at a restaurant (name, job role, PIN hash, `pos_id`), the row the staff portal signs in against.
- `membership_pin_attempts`, `portal_attempts`, `portal_nonces`: PIN lockout, portal throttling, one-time nonces. A lockout lasts 15 min × 2^(lockouts in the last day), capped at 24 h (`lockout_count`, `last_locked_at`); `portal_attempts.ok=1` marks a hit that ended well, which counts toward the 300-per-5-min ceiling but not the 30 failures (SEC-18/19).
- `two_fa_challenges`: one row per 2FA sign-in attempt (`purpose='login'`) or per login's "Send test code" (`'setup'`); the code and the pending secret are keyed hashes only, single-use, 10 minutes. `restaurants.two_fa_code/two_fa_pending/two_fa_expires` are no longer read and are blanked at boot (SEC-20/39).
- `view_as_sessions`: who opened each admin view-as session (by session-token hash) and whether it is read-only (opened by support; SEC-12).
- `staff_portal_tokens`: the per-restaurant portal links; `staff_signups`: the claim-your-name flow.
- `permission_grants`, `login_prefs`: see *Strategic foundations* below.

## Reviews

### `reviews` (models.py)
`restaurant_id` + `platform` (google/yelp/csv/manual) + `external_id` — **unique on `(restaurant_id, platform, external_id)`**, deliberately including `restaurant_id` in the key (a global-unique key let two tenants sharing a `google_place_id` silently steal each other's reviews). Carries the raw review (`author`, `rating` 1–5, `text`, `review_date`, `fetched_at`), Claude's analysis (`sentiment`, `categories` JSON, `summary`, `urgency`), and the response workflow (`draft_response`, `response_status`: pending/drafted/approved/posted/skipped, `approved_at`, `posted_at`, `draft_edited`, `regenerate_count`, `deleted_at`).

### `review_requests`
Outbound "please leave us a review" asks — tracks `customer_phone`, send state.

### `response_templates`
Saved canned-response templates per restaurant.

## Labor / Scheduling

### `schedule_history`
One row per generated schedule: `week_start`/`week_end`, `hours_scheduled`/`hours_budget`/`labor_target`, the full `schedule_csv`, `summary_json`, and (added later) `quality_json` — the Shift Quality Engine's full evaluation for that schedule — plus `edited_at`/`edited_by` when a manager hand-edits a published schedule. `published_at`/`published_by` mark the moment it went to staff: the staff portal, the payroll-week hours check and `schedule_publish_trust` read only rows with `published_at` set — a draft is never "the schedule". `review_json` is the rule sweep (`schedule_rules.summarize`), `generation_seconds` the model time, `weather_json` the forecast the draft saw.

### `schedule_versions`
Every state a schedule has been in — `(history_id, version)` unique, `reason` in `generated | edited | fixes | published`, the CSV, `quality_json`, `diff_json` against the previous version, `saved_by`. `schedule_versions.learned_patterns` reads the recurring edits back into the next draft.

### `staff_settings` / `staff_pairs`
The roster's facts: `active` (a deactivated person is never scheduled, whatever the shift history says), `employment_type` (full/part), `min_hours`/`max_hours`, `daypart_availability` (JSON weekday → any/morning/night/off), `is_minor`. Keyed by `(restaurant_id, employee_name)` like every staff table. `staff_pairs` is `prefer`/`avoid` between two names, stored once whichever way round it was typed.

### `demand_signals`
Owner-entered facts about specific dates: `kind` event or reservations, `label`, `covers`, `lift_pct`, `source` (manual, csv, or a future sync). Unique on `(restaurant_id, date, kind, label)`.

### `shift_change_requests`
A member of staff asking to drop a published shift: `history_id`, the shift, `reason`, `status` in `pending | open | denied | covered | withdrawn`, `replacement_name`, `decided_by`. `covered` also rewrites the published CSV and appends a version.

### `schedule_outcomes`
One row per published week, date and daypart: scheduled hours and people, the day's sales split by the intraday morning share, labor %, coverage and no-show issues that day, the mean review rating. Written by the Monday job (`schedule_intel.record_outcomes`), keyed `(history_id, date, daypart)`; read into the prompt as "what published weeks actually did" and by the chemistry suggestions and the cohort ratio features.

### `schedule_recommendation_events` / `schedule_pattern_dismissals` / `staff_first_seen`
The accept/dismiss ledger for recommendations (a kind shown ten times and never accepted stops being shown); the owner's dismissed learned patterns; and the earliest date and most shifts ever seen per name, so tenure survives the rolling upload window.

### `staff_settings` (extended) / `shift_change_requests` (extended)
`time_windows` (per-weekday earliest/latest), `certifications`, and the employee's own `preferred_dayparts` and `desired_hours`. Shift requests carry `kind` (drop | swap) and the target shift for a swap.

### `schedule_history` (extended)
`quality_score`, `quality_band`, `quality_confidence` (the headline, so the list never parses the blob), `what_if_json`, `superseded_by` (a draft replaced by a newer draft of the same week), `republished_at` (an edit to a sent week re-notified the people whose shifts changed).

### `restaurants.compliance_json` / `restaurants.role_floors_json` and the new columns
`jurisdiction` (a compliance pack code), `role_arrival_json`, `role_requirements_json`, `foh_roles_json`, `patio_roles_json`, `trim_to_budget`, `reservation_provider`, `reservation_api_key`.
The scheduling rules (`schedule_rules.DEFAULTS` keys) and the per-role, per-daypart staffing floors — the setting that replaced the compiled-in pizza-cook rule.

### `staff_capabilities`
The Operational Score layer. `(restaurant_id, employee_name, attribute)` unique — `attribute` is things like `overall`, `can_close`, per-role scores. `score` (1–5 scale) or `flag` (boolean), `notes`, `updated_by`. Unrated is absent, never a row with a zero score — a name only appears here once someone has actually rated them.

### `capability_changes`
Audit trail of every capability edit (who changed what score, when) — feeds the "why is this schedule different" explanation path.

### `staff_availability`
Days an employee has said they can't work, submitted via their own schedule link — the AI scheduler treats this as equal priority to hard staff constraints.

### `staff_contacts` / `staff_notes`
Contact info for schedule delivery; freeform per-employee notes.

### `labor_history` / `labor_daily_history`
Per-period and per-day labor % / sales / hours — the source for the Labor ribbon chart and Ask Cavnar's "how is today going" answer.

### `schedule_shares` (share links)
Public, expiring links a schedule is published to for staff to view (and submit availability against). Expiry is ~60 days out, pushed back on republish; an expired link is flagged rather than serving stale data or vanishing.

### `staff_time_off`, `covers_daily`, `manual_team_members`, `team_messages`, `task_templates` / `task_completions`
Time-off requests (owner decides in place); covers per day for the labor-per-cover read; team members added by hand where the POS has none; the owner↔staff message threads; and today's flat task checklist per job role — the thing `docs/plans/TASK_SHEETS_PLAN.md` replaces.

### `shift_profiles`
Demand-level profiles (low/normal/high/peak) per daypart, used by the Shift Quality Engine's demand-match dimension.

### Overstaffed days / overtime
Computed on read from `labor_daily_history` + shift data, not a table — see `labor.py`.

## Ask Cavnar

### `ask_cavnar_conversations` / `ask_cavnar_messages`
One conversation thread per owner session; messages carry role (user/assistant), content, tool-call records.

### `ask_cavnar_actions`
Logged write-tool proposals and whether they were confirmed — the audit trail for anything Ask Cavnar was asked to *do* rather than just answer. A proposal row's own `id` is its proposal id (sent to clients as `proposal_id`, rec_ledger key `ask:<id>`); a confirm/dismiss row carries the proposal it answers in `proposal_id` (NULL from clients older than that — those settle by action + summary), and a dismissal's optional `reason`.

### `ask_memory`
Durable facts an owner has told the assistant across conversations. `(restaurant_id, fact)` unique (idempotent — saying the same thing twice doesn't duplicate it), `kind` (goal/context/preference/followup), `source`, capped at `ASK_MEMORY_LIMIT` per restaurant (oldest falls off) so it can't grow the prompt unboundedly.

## Alerts / Notifications

### `alert_log`
Every notification that has fired: `restaurant_id`, `alert_type`, optional `review_id`, `fired_at`, `value` (the figure it fired on), `priority` (`push.PRIORITY`, P0–P5, stamped at write time). Read back by both notification centers and Ask Cavnar's `read_alerts` tool — "outstanding" is computed by joining to the linked review's `response_status`, not stored as a separate flag.

It is two things at once: the HISTORY both clients read, and the tally the daily cap and the 50/day ceiling are counted from. The advisory types in `models.NON_ALERT_TYPES` (brief, pulse, closing summary, drafted schedule, issues, coverage, outcome wins, the `daily_briefing` wrapper) write history rows but are excluded from `count_alerts_today` — they are not alerts, and an issue text goes to a manager rather than to the owner whose cap it would spend.

### `alert_holds`
Alerts raised mid-service, waiting for the rush to end: `alert_type`, `subject`, `html`, `sms_text`, `review_id`, `value`, `release_at`, `sent_at`, and `meta_json` (the recommendation keys, push audience and brief-covered types the release must honour). `release_due_alerts` sends at most `MAX_RELEASE_PER_RESTAURANT` per pass and drops anything `HOLD_MAX_LATE_HOURS` past its release.

### `notification_reads` / `notification_opens`
Per-LOGIN read state (PK `user_id, restaurant_id`) and per-type open events. An open carries `alert_log_id` (from the push payload, accepted only when that row is this restaurant's — so time-to-open is `opened_at - alert_log.fired_at`) and `rec_key`. `seen_at` is written in SQLite's own `"%Y-%m-%d %H:%M:%S"` **deliberately** — the old stamp on `restaurants.notifications_seen_at` used isoformat's `T`, and since the unread query is a TEXT comparison and `' ' < 'T'`, every alert fired on the same date as the last read counted as already seen. The badge could only ever show yesterday.

### `alert_contacts`
Who gets alerted and on which channels (email/SMS/push), per restaurant — separate from the login `users` table since an alert recipient need not have dashboard access.

### `device_tokens` / `push_deliveries`
APNs tokens per user/restaurant; delivery attempts and outcomes for push. `disabled_reason` PARKS a token rather than deleting it — `get_device_tokens(for_delivery=True)` skips it and the next app launch re-registers and clears it. Only Apple's own "this token is gone" (`_PERMANENT_FAILURE_REASONS`) deletes a row.

### `home_dismissals`
Attention items a user has dismissed from the Home brief, so a handled issue doesn't keep resurfacing.

## Marketing

`marketing_drafts`, `marketing_scheduled_posts`, `marketing_content_log` (each row tagged with `menu_item_id`, `occasion`, `post_kind`), `marketing_media`, `marketing_links`, `marketing_attribution`, `content_calendar_cache`, `guest_campaigns`, `guest_campaign_recipients` (which guests each campaign reached, matched back through the connected POS), `guest_contacts`, `sms_optin_invites`.

## Food Cost / Inventory

`ingredients`, `ingredient_stock_events`, `inventory_history`, `menu_items`, `menu_item_sales` (per-item units from the POS, the basis of dish lift and depletion), `recipe_ingredients`, `recipe_drafts` (model-drafted, accepted line by line), `purchase_orders`, `forecast_log` (every waste forecast, scored later), `food_cost_diagnoses` and `review_diagnoses` (the stored root-cause reads the 6am jobs write).

## The owner's day

- `alert_holds` — an alert raised mid-service, waiting for the rush to end (`release_at` UTC, `sent_at` once handled or dropped).
- `close_outs` — one per restaurant per business date; a close-out filed after midnight belongs to the night before.
- `action_snoozes` — PK (restaurant_id, key); "not today" is a date, never a dismissal.
- `pos_intraday` — PK (restaurant_id, business_date, captured_hour); net sales so far, and the same weekday/hour profile it builds for later weeks.
- `restaurants` columns: `alert_hold_during_service` (default on), `preshift_nudge_hour` (0 = off).

## Strategic foundations

- `recommendation_outcomes` — one tracked change: metric, baseline window/value/detail, `evaluate_on`, verdict. Partial unique index on (restaurant_id, source_key) while `status='tracking'`.
- `owner_goals` — target per metric, one `active` per metric (older rows become `replaced`).
- `ops_issues` — issue, assignee contact, status open→acknowledged→resolved, `escalation_contact_id`, `escalated_at`, `resolution_note`, `notify_suppressed` (filed with notify=False — `issues.tick` never texts it; reassigning by name lifts it), `meta_json` (e.g. a coverage issue's suggested covers and who was asked). Timestamps UTC `YYYY-MM-DD HH:MM:SS`.
- `issue_links` — `token_hash` → (issue, contact, purpose). One link per person; tokens are never stored.
- `issue_routing` — PK (restaurant_id, role ∈ manager|escalation) → consented contact, `escalate_after_minutes`.
- `pos_loss_daily` — PK (restaurant_id, business_date, kind); zero rows are written for every day×kind asked about, so absence means "never asked".
- `invoice_imports` — one scanned invoice: `image_sha` (dedupe), `lines_json` (proposal), `applied_json` (old→new costs), `applied_at` (set once).
- `permission_grants` (auth schema) — PK (user_id, restaurant_id, permission); owner-granted extras beyond a role, `permissions.GRANTABLE` only (foodcost.view, loss.view).
- `login_prefs` (auth schema) — PK (user_id, restaurant_id); `morning_brief` NULL = role default (owners and managers on, teammates off).
- `restaurants` columns: `morning_brief_enabled`, `morning_brief_hour`, `auto_draft_schedule`, `external_scheduling_tool` (all four touch points).

## Intel

`competitor_snapshots` (week-over-week diffed), `ai_visibility_runs` (one row per run, with `answered`/`appeared` totals, the `city_basis` it was measured against — runs on different bases are never compared — and the `payload_json` the Intel tab serves after a redeploy) / `ai_visibility_query_runs` (per query: `appeared` and the `answer` text).

## Billing / Contracts / Webhooks

- `docusign_events_seen`, `stripe_events_seen` — inbound-webhook idempotency, event id de-dup before acting.
- `webhooks` — one row per client-configured outbound webhook (`url`, `secret`, `events` JSON list, `is_active`, `consecutive_failures`/`disabled_reason` for auto-disable after repeated failures).
- `webhook_deliveries` — attempt log (`status`, `ok`, `attempts`, `error`) for each outbound fire.

## Admin / Ops

`admin_events`, `admin_issue_resolutions`, `job_runs`, `job_failures`, `job_period_claims`, `job_cursors` (where a bounded pass stopped), `scheduler_lease`, `milestones` (firsts an owner is told about once), `activity_log`, `async_jobs`, `ai_usage` (per-call cost/token logging from `ai_utils.log_ai_usage`), `changelog_entries`, `status_incidents` / `status_incident_updates` / `service_status` (the public status page), `sales_audits` / `sales_audit_shares` (the in-person sales tool), `value_snapshots` (daily "value delivered" figure, populated opportunistically on first Home-tab load of the day — not a scheduled job).

## Email

`email_log` (every send, for the Account "email history" view and debugging), `email_suppressions` (bounces/opt-outs), `onboarding_emails` (day-2/7/30 sequence state), `weekly_reports`, `login_reports`, `client_data` (miscellaneous cached client-facing snapshots).

## Conventions that apply across this schema

- **Additive-only migrations.** A new column is added via a guarded `ALTER TABLE` in `init_db()`'s list or `ensure_columns()`'s (both run at boot; 57 columns live only in the latter), never a destructive rewrite, so old rows keep working and a rollback of the code doesn't orphan data.
- **`restaurant_id` scopes every tenant table**, and the newer tables declare `NOT NULL REFERENCES restaurants(id)`. About thirty older tables (`alert_log`, `schedule_history`, `email_log`, `sessions`, `login_history`, `ai_usage`, `push_deliveries`, …) carry the column without the FK clause, so `PRAGMA foreign_keys=ON` does not protect them; the `WHERE ... AND restaurant_id=?` convention does. New tables declare the FK.
- **Append-only tables stay append-only** (`login_history`, `ask_cavnar_actions`, `alert_log`, `capability_changes`) — they are audit trails; a "current state" view is always computed by querying the latest row, not by mutating history in place.
- **JSON-in-a-column** (`*_json` fields on `restaurants`, `summary_json` on `schedule_history`) is used deliberately for structured settings that don't need their own table and are always read/written as a whole blob by one piece of code — not for anything queried by its internal fields.

## Intelligence engine

`restaurants.category` (the cohort; inferred when unset), `intel_features`
(one row per restaurant-week of ratios, rates and counts — the only table
cross-restaurant learning reads; no dollars, names or people),
`intel_rec_events` (every recommendation's presented / answered / measured
events, derived from `home_dismissals`, `recommendation_outcomes`,
`ask_cavnar_actions` and `delayed_actions`), `intel_patterns` (discovered
patterns with n, effect, p, q, confidence, status active|retired),
`intel_benchmarks` (cohort × metric × week percentiles, n ≥ 5),
`intel_confidence_log` (weekly acceptance and success by kind). Invariant:
no row in `intel_patterns`, `intel_benchmarks` or `intel_confidence_log`
describes fewer than `privacy.MIN_COHORT` restaurants.
