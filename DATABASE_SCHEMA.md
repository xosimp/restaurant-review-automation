# Database Schema — Cavnar AI

Single SQLite database (`reviews.db`), WAL mode, on a Railway persistent volume in production. Schema lives across `models.py` (`init_db()` + `ensure_columns()`), `auth.py` (`init_auth()`), `push.py` (`init_push()`), `webhooks.py` (`init_webhooks()`), and a handful of lazy-`init_*()` functions for tables that didn't exist from day one (`init_ask_memory`, `init_two_fa_backup_codes`, `init_capability_changes`, ...). There is no version table — migrations are additive `ALTER TABLE ADD COLUMN` statements guarded by `try/except`, run on every boot. **77 tables** as of this writing.

`restaurant_id` is the tenant boundary on essentially every table below it — every read must filter by it from the authenticated session, never from client-supplied input.

## Core identity

### `restaurants` (models.py)
The tenant row. Everything else hangs off `restaurants.id`.
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
- `users`: one row per login, `restaurant_id` FK, `username`/`email` unique, `password_hash`, `is_admin`, `is_active`, `role` (owner/teammate — gates team invite/revoke).
- `sessions`: `token` PK, `expires_at`, `last_active`, `ip_address`, `user_agent` — hard-deleted on expiry/revoke/device-dedup.
- `login_history`: append-only, **never** pruned — independent of `sessions`, this is what the Account "sign-in activity" view reads, so a login stays visible regardless of what later happens to the session it produced.
- `trusted_devices`: "remember this device 30 days" for 2FA, one row per device (superseded a single-column approach that only held the last device).
- `two_fa_backup_codes`: hashed, single-use fallback codes generated at 2FA setup.

## Reviews

### `reviews` (models.py)
`restaurant_id` + `platform` (google/yelp/csv/manual) + `external_id` — **unique on `(restaurant_id, platform, external_id)`**, deliberately including `restaurant_id` in the key (a global-unique key let two tenants sharing a `google_place_id` silently steal each other's reviews). Carries the raw review (`author`, `rating` 1–5, `text`, `review_date`, `fetched_at`), Claude's analysis (`sentiment`, `categories` JSON, `summary`, `urgency`), and the response workflow (`draft_response`, `response_status`: pending/drafted/approved/posted/skipped, `approved_at`, `posted_at`, `draft_edited`, `regenerate_count`, `deleted_at`).

### `review_requests`
Outbound "please leave us a review" asks — tracks `customer_phone`, send state.

### `response_templates`
Saved canned-response templates per restaurant.

## Labor / Scheduling

### `schedule_history`
One row per generated schedule: `week_start`/`week_end`, `hours_scheduled`/`hours_budget`/`labor_target`, the full `schedule_csv`, `summary_json`, and (added later) `quality_json` — the Shift Quality Engine's full evaluation for that schedule — plus `edited_at`/`edited_by` when a manager hand-edits a published schedule.

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

### `schedule_shares` / `staff_schedule_publish` (share links)
Public, expiring links a schedule is published to for staff to view (and submit availability against). Expiry is ~60 days out, pushed back on republish; an expired link is flagged rather than serving stale data or vanishing.

### `shift_profiles`
Demand-level profiles (low/normal/high/peak) per daypart, used by the Shift Quality Engine's demand-match dimension.

### `overstaffed_days` / overtime tracking
Computed on read from `labor_daily_history` + shift data, not a standalone table — see `labor.py`.

## Ask Cavnar

### `ask_cavnar_conversations` / `ask_cavnar_messages`
One conversation thread per owner session; messages carry role (user/assistant), content, tool-call records.

### `ask_cavnar_actions`
Logged write-tool proposals and whether they were confirmed — the audit trail for anything Ask Cavnar was asked to *do* rather than just answer.

### `ask_memory`
Durable facts an owner has told the assistant across conversations. `(restaurant_id, fact)` unique (idempotent — saying the same thing twice doesn't duplicate it), `kind` (goal/context/preference/followup), `source`, capped at `ASK_MEMORY_LIMIT` per restaurant (oldest falls off) so it can't grow the prompt unboundedly.

## Alerts / Notifications

### `alert_log`
Every alert that has fired: `restaurant_id`, `alert_type`, optional `review_id`, `fired_at`. Read back by both the notification badge and Ask Cavnar's `read_alerts` tool — "outstanding" is computed by joining to the linked review's `response_status`, not stored as a separate flag.

### `alert_contacts`
Who gets alerted and on which channels (email/SMS/push), per restaurant — separate from the login `users` table since an alert recipient need not have dashboard access.

### `device_tokens` / `push_deliveries`
APNs tokens per user/restaurant; delivery attempts and outcomes for push.

### `home_dismissals`
Attention items a user has dismissed from the Home brief, so a handled issue doesn't keep resurfacing.

## Marketing

`marketing_drafts`, `marketing_scheduled_posts`, `marketing_content_log`, `marketing_media`, `marketing_links`, `marketing_attribution`, `content_calendar_cache`, `guest_campaigns`, `guest_contacts`, `sms_optin_invites`.

## Food Cost / Inventory

`ingredients`, `ingredient_stock_events`, `inventory_history`, `menu_items`, `recipe_ingredients`, `purchase_orders`, `statements`.

## Intel

`competitor_snapshots` (week-over-week diffed), `ai_visibility_runs` / `ai_visibility_query_runs` (Perplexity-backed "do LLMs mention us" checks, `answered`/`appeared` columns per query).

## Billing / Contracts / Webhooks

- `docusign_events_seen`, `stripe_events_seen` — inbound-webhook idempotency, event id de-dup before acting.
- `webhooks` — one row per client-configured outbound webhook (`url`, `secret`, `events` JSON list, `is_active`, `consecutive_failures`/`disabled_reason` for auto-disable after repeated failures).
- `webhook_deliveries` — attempt log (`status`, `ok`, `attempts`, `error`) for each outbound fire.

## Admin / Ops

`admin_events`, `admin_issue_resolutions`, `job_runs`, `job_failures`, `job_period_claims`, `scheduler_lease`, `activity_log`, `async_jobs`, `ai_usage` (per-call cost/token logging from `ai_utils.log_ai_usage`), `changelog_entries`, `status_incidents` / `status_incident_updates` / `service_status` (the public status page), `sales_audits` / `sales_audit_shares` (the in-person sales tool), `value_snapshots` (daily "value delivered" figure, populated opportunistically on first Home-tab load of the day — not a scheduled job).

## Email

`email_log` (every send, for the Account "email history" view and debugging), `email_suppressions` (bounces/opt-outs), `onboarding_emails` (day-2/7/30 sequence state), `weekly_reports`, `login_reports`, `client_data` (miscellaneous cached client-facing snapshots).

## Conventions that apply across this schema

- **Additive-only migrations.** A new column is added via `ensure_columns()`'s guarded `ALTER TABLE`, never a destructive rewrite, so old rows keep working and a rollback of the code doesn't orphan data.
- **`restaurant_id NOT NULL REFERENCES restaurants(id)`** on every tenant-scoped table — enforced by the FK, not just convention.
- **Append-only tables stay append-only** (`login_history`, `ask_cavnar_actions`, `alert_log`, `capability_changes`) — they are audit trails; a "current state" view is always computed by querying the latest row, not by mutating history in place.
- **JSON-in-a-column** (`*_json` fields on `restaurants`, `summary_json` on `schedule_history`) is used deliberately for structured settings that don't need their own table and are always read/written as a whole blob by one piece of code — not for anything queried by its internal fields.
